import sys
import json
import os
import traceback
from dotenv import load_dotenv

# Load local environment variables (.env) if present
load_dotenv()

# List of tools and their JSON schemas matching the MCP specification
TOOLS = [
    {
        "name": "list_stations",
        "description": "احصل على قائمة بجميع محطات الراديو المهيأة وتفاصيلها الأساسية مثل الاسم والحالة ورابط الاستماع.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "get_now_playing",
        "description": "احصل على الأغنية التي تعمل حالياً وبيانات البث لجميع محطات الراديو.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "get_station_playlists",
        "description": "احصل على قائمة قوائم التشغيل (Playlists) لعمليات بث محددة عبر رقم المحطة (ID).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "station_id": {"type": "integer", "description": "رقم تعريف المحطة (Station ID)"}
            },
            "required": ["station_id"]
        }
    },
    {
        "name": "get_station_files",
        "description": "استعراض ملفات الصوت والميديا المخزنة لمحطة محددة.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "station_id": {"type": "integer", "description": "رقم تعريف المحطة (Station ID)"}
            },
            "required": ["station_id"]
        }
    },
    {
        "name": "get_station_listeners",
        "description": "احصل على تفاصيل المستمعين الحاليين ومواقعهم الجغرافية لمحطة محددة.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "station_id": {"type": "integer", "description": "رقم تعريف المحطة (Station ID)"}
            },
            "required": ["station_id"]
        }
    },
    {
        "name": "get_weather",
        "description": "احصل على حالة الطقس الحالية والبيانات الجوية المرتبطة بالسيرفر.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "get_system_logs",
        "description": "استعراض سجل العمليات والوصول الأخير إلى لوحة تحكم السيرفر (Access Logs).",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    }
]

def handle_tool_call(name, args):
    """
    Executes the designated tool by sending API requests either to AzuraCast 
    or back to the local Flask application.
    """
    base_url = os.environ.get("AZURACAST_BASE_URL", "https://radio.zad.tools")
    api_key = os.environ.get("AZURACAST_API_KEY", "")
    
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        
    import requests
    
    try:
        if name == "list_stations":
            r = requests.get(f"{base_url}/api/stations", headers=headers, timeout=10)
            if r.status_code == 200:
                stations = r.json()
                out = []
                for s in stations:
                    out.append({
                        "id": s.get("id"),
                        "name": s.get("name"),
                        "shortcode": s.get("shortcode"),
                        "listen_url": s.get("listen_url"),
                        "is_active": s.get("is_active")
                    })
                return json.dumps(out, ensure_ascii=False, indent=2)
            return f"Failed to fetch stations from AzuraCast: {r.status_code} - {r.text}"
            
        elif name == "get_now_playing":
            r = requests.get(f"{base_url}/api/nowplaying", headers=headers, timeout=10)
            if r.status_code == 200:
                return json.dumps(r.json(), ensure_ascii=False, indent=2)
            return f"Failed to fetch now playing: {r.status_code} - {r.text}"
            
        elif name == "get_station_playlists":
            sid = args.get("station_id")
            r = requests.get(f"{base_url}/api/station/{sid}/playlists", headers=headers, timeout=10)
            if r.status_code == 200:
                return json.dumps(r.json(), ensure_ascii=False, indent=2)
            return f"Failed: {r.status_code} - {r.text}"
            
        elif name == "get_station_files":
            sid = args.get("station_id")
            r = requests.get(f"{base_url}/api/station/{sid}/files", headers=headers, timeout=10)
            if r.status_code == 200:
                return json.dumps(r.json(), ensure_ascii=False, indent=2)
            return f"Failed: {r.status_code} - {r.text}"
            
        elif name == "get_station_listeners":
            sid = args.get("station_id")
            r = requests.get(f"{base_url}/api/station/{sid}/listeners", headers=headers, timeout=10)
            if r.status_code == 200:
                return json.dumps(r.json(), ensure_ascii=False, indent=2)
            return f"Failed: {r.status_code} - {r.text}"
            
        elif name == "get_weather":
            port = os.environ.get("PORT", "4180")
            r = requests.get(f"http://127.0.0.1:{port}/api/v1/weather", timeout=5)
            if r.status_code == 200:
                return json.dumps(r.json(), ensure_ascii=False, indent=2)
            return f"Failed to get weather: {r.status_code}"
            
        elif name == "get_system_logs":
            port = os.environ.get("PORT", "4180")
            guide_token = os.environ.get("GUIDE_TOKEN", "")
            headers_local = {}
            if guide_token:
                headers_local["X-Guide-Token"] = guide_token
            r = requests.get(f"http://127.0.0.1:{port}/api/v1/auth/logs", headers=headers_local, timeout=5)
            if r.status_code == 200:
                return json.dumps(r.json(), ensure_ascii=False, indent=2)
            return f"Failed to fetch logs: {r.status_code} - {r.text}"
            
        else:
            return f"Unknown tool: {name}"
            
    except Exception as e:
        return f"Error executing tool {name}: {str(e)}\n{traceback.format_exc()}"

def handle_mcp_message(req):
    """
    Handles a single incoming MCP/JSON-RPC request dictionary 
    and returns the corresponding response dictionary (or None).
    """
    msg_id = req.get("id")
    method = req.get("method")
    params = req.get("params", {})
    
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {}
                },
                "serverInfo": {
                    "name": "radio-control",
                    "version": "1.0.0"
                }
            }
        }
        
    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "tools": TOOLS
            }
        }
        
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments", {})
        result_str = handle_tool_call(name, args)
        
        is_error = False
        if result_str.startswith("Error") or result_str.startswith("Failed"):
            is_error = True
            
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": result_str
                    }
                ],
                "isError": is_error
            }
        }
        
    return None

def stdio_loop():
    """
    Main loop for stdio transport. Exchanged JSON-RPC over stdin/stdout.
    """
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        try:
            req = json.loads(line)
            res = handle_mcp_message(req)
            if res:
                sys.stdout.write(json.dumps(res) + "\n")
                sys.stdout.flush()
        except Exception:
            pass

if __name__ == "__main__":
    stdio_loop()
