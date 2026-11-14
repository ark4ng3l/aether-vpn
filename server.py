import os
import json
import time
import socket
import base64
import urllib.request
import urllib.parse
import datetime
import concurrent.futures
from fastapi import FastAPI, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, PrivateFormat, NoEncryption
import qrcode
from io import BytesIO

app = FastAPI(title="Aether VPN Hub & Manager")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(PROJECT_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)

# Cache for free configs
CACHE = {
    "warp": None,
    "clean_ips": [],
    "free_configs": [],
    "last_sub_fetch": 0
}

@app.get("/api/status")
def get_status():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    lan_ip = "127.0.0.1"
    try:
        s.connect(("8.8.8.8", 80))
        lan_ip = s.getsockname()[0]
    except Exception:
        pass
    finally:
        s.close()

    return {
        "status": "online",
        "app_name": "Aether VPN Hub & Manager",
        "version": "1.0.0",
        "lan_ip": lan_ip,
        "port": 3101,
        "phone_sub_url": f"http://{lan_ip}:3101/api/sub",
        "cached_configs_count": len(CACHE["free_configs"]),
        "has_warp": CACHE["warp"] is not None
    }

@app.get("/api/warp/generate")
def generate_warp():
    try:
        priv = x25519.X25519PrivateKey.generate()
        pub = priv.public_key()
        priv_b64 = base64.b64encode(priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode()
        pub_b64 = base64.b64encode(pub.public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()

        now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds')
        payload = {
            "key": pub_b64,
            "install_id": "",
            "fcm_token": "",
            "tos": now,
            "model": "PC",
            "serial_number": "",
            "locale": "en_US"
        }
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            "https://api.cloudflareclient.com/v0a2158/reg",
            data=data,
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "User-Agent": "okhttp/3.12.1"
            }
        )
        reg_data = None
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                reg_data = json.loads(resp.read().decode('utf-8'))
        except Exception:
            proxy_handler = urllib.request.ProxyHandler({'http': 'http://127.0.0.1:10808', 'https': 'http://127.0.0.1:10808'})
            opener = urllib.request.build_opener(proxy_handler)
            with opener.open(req, timeout=8) as resp:
                reg_data = json.loads(resp.read().decode('utf-8'))

        config = reg_data.get("config", {})
        peers = config.get("peers", [{}])[0]
        cf_public_key = peers.get("public_key", "bmXOC+F1FxEMF9dyiK2H5/1SUtzH0JuVo51h2wPfgyo=")
        addresses = config.get("interface", {}).get("addresses", {})
        v4_addr = addresses.get("v4", "172.16.0.2/32")
        v6_addr = addresses.get("v6", "")

        best_endpoint = "162.159.192.1:2408"

        clean_addrs = f"{v4_addr}"
        if v6_addr:
            clean_addrs += f",{v6_addr}"

        wg_uri = (
            f"wireguard://{urllib.parse.quote(priv_b64)}@{best_endpoint}"
            f"?publickey={urllib.parse.quote(cf_public_key)}"
            f"&address={urllib.parse.quote(clean_addrs)}"
            f"&reserved=0%2C0%2C0&mtu=1420"
            f"#%E2%9A%A1%20Aether%20WARP%20%7C%20Free%20Clean%20IP"
        )

        conf_text = f"""[Interface]
PrivateKey = {priv_b64}
Address = {clean_addrs}
DNS = 1.1.1.1, 8.8.8.8

[Peer]
PublicKey = {cf_public_key}
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = {best_endpoint}
"""

        CACHE["warp"] = {
            "private_key": priv_b64,
            "public_key": cf_public_key,
            "v4": v4_addr,
            "v6": v6_addr,
            "endpoint": best_endpoint,
            "uri": wg_uri,
            "conf": conf_text
        }

        return {"success": True, "data": CACHE["warp"]}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/api/scan-clean-ip")
def scan_clean_ip():
    candidate_ips = [
        ("162.159.192.1", 2408), ("162.159.192.2", 2408), ("162.159.192.5", 2408),
        ("162.159.193.1", 2408), ("162.159.193.5", 2408), ("162.159.195.1", 2408),
        ("188.114.96.1", 2408), ("188.114.97.1", 2408), ("188.114.98.1", 2408),
        ("104.16.132.229", 443), ("104.17.150.1", 443), ("104.21.30.1", 443),
        ("172.67.180.1", 443), ("172.67.70.1", 443)
    ]

    def test_one(item):
        ip, port = item
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM if port == 2408 else socket.SOCK_STREAM)
            s.settimeout(0.6)
            t0 = time.time()
            s.connect((ip, port))
            s.send(b"\x00")
            lat = (time.time() - t0) * 1000
            s.close()
            return {"ip": ip, "port": port, "latency": round(lat, 1), "type": "UDP" if port == 2408 else "TCP"}
        except Exception:
            return None

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as ex:
        futures = [ex.submit(test_one, item) for item in candidate_ips]
        for f in concurrent.futures.as_completed(futures):
            res = f.result()
            if res:
                results.append(res)

    results.sort(key=lambda x: x["latency"])
    CACHE["clean_ips"] = results
    return {"success": True, "count": len(results), "clean_ips": results}

@app.get("/api/free-subs")
def get_free_subs(limit: int = 30):
    now = time.time()
    if CACHE["free_configs"] and (now - CACHE["last_sub_fetch"] < 300):
        return {"success": True, "cached": True, "configs": CACHE["free_configs"][:limit]}

    url = "https://raw.githubusercontent.com/barry-far/V2ray-config/main/Splitted-By-Protocol/vless.txt"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'v2rayNG/1.8'})
        with urllib.request.urlopen(req, timeout=6) as r:
            lines = r.read().decode('utf-8', errors='ignore').splitlines()

        parsed = []
        for line in lines:
            line = line.strip()
            if line.startswith("vless://"):
                remark = "Aether Node"
                if "#" in line:
                    remark = urllib.parse.unquote(line.split("#", 1)[1])
                is_reality = "security=reality" in line
                parsed.append({
                    "uri": line,
                    "remark": remark,
                    "protocol": "VLESS",
                    "type": "Reality" if is_reality else "WS/TCP"
                })

        CACHE["free_configs"] = parsed
        CACHE["last_sub_fetch"] = now
        return {"success": True, "count": len(parsed), "configs": parsed[:limit]}
    except Exception as e:
        return {"success": False, "error": str(e), "configs": CACHE["free_configs"][:limit]}

@app.get("/api/qr")
def get_qr(text: str):
    img = qrcode.make(text)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(content=buf.getvalue(), media_type="image/png")

@app.get("/api/sub")
def serve_subscription():
    nodes = []

    if CACHE.get("warp"):
        nodes.append(CACHE["warp"]["uri"])

    if not CACHE["free_configs"]:
        get_free_subs(limit=40)

    for item in CACHE["free_configs"][:40]:
        nodes.append(item["uri"])

    sub_text = "\n".join(nodes)
    encoded = base64.b64encode(sub_text.encode('utf-8')).decode('utf-8')

    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "Subscription-Userinfo": "upload=100; download=200; total=1073741824000; expire=2546249531",
        "Profile-Title": "base64:" + base64.b64encode("Aether VPN Hub".encode('utf-8')).decode('utf-8'),
        "Profile-Update-Interval": "2"
    }
    return Response(content=encoded, media_type="text/plain", headers=headers)

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

if __name__ == "__main__":
    print("[*] Starting Aether VPN Hub on port 3101...")
    uvicorn.run("server:app", host="0.0.0.0", port=3101, reload=False)
