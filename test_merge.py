"""Integration test for the file-based personas merge. No AzuraCast network needed."""
import os, sys, json

os.environ["AZURACAST_API_KEY"] = ""        # routes we hit don't call AzuraCast
os.environ["WERKZEUG_RUN_MAIN"] = "false"
os.environ.setdefault("OPENAI_API_KEY", "")

import app as appmod

ok = True
def check(label, cond):
    global ok; ok = ok and cond
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")

c = appmod.app.test_client()

print("de-hardcoded:")
check("STATION_TEMPLATES removed from app module", not hasattr(appmod, "STATION_TEMPLATES"))
check("build_station_templates imported into app", hasattr(appmod, "build_station_templates"))

print("templates endpoint (built from files):")
r = c.get("/api/v1/templates").get_json()
check("ok", r["ok"])
check("4 stations", len(r["data"]) == 4)
keys = [s["key"] for s in r["data"]]
check("station order preserved", keys == ["islamic", "science_tech", "motivation_sports", "history_stories"])
islamic = next(s for s in r["data"] if s["key"] == "islamic")
check("show structure intact", set(islamic["shows"]["morning"]) >=
      {"description", "start_time", "end_time", "system_prompt", "user_prompt_template"})
check("persona text intact", "مذيع النور" in islamic["shows"]["morning"]["system_prompt"])
check("times are 4-char strings", islamic["shows"]["morning"]["start_time"] == "0500")

print("agents edit API:")
ra = c.get("/api/v1/agents").get_json()
check("16 agents listed", ra["ok"] and len(ra["data"]) == 16)
g = c.get("/api/v1/agents/islamic/morning").get_json()
check("get raw file content", g["ok"] and "system_prompt" not in g["data"]["content"] and "مذيع النور" in g["data"]["content"])

# hot-reload edit round-trip
orig = g["data"]["content"]
edited = orig.rstrip() + " (تعديل اختبار)\n"
pr = c.put("/api/v1/agents/islamic/morning", json={"content": edited}).get_json()
check("PUT edit ok", pr["ok"])
r2 = c.get("/api/v1/templates").get_json()
isl2 = next(s for s in r2["data"] if s["key"] == "islamic")
check("edit reflected in /templates (hot reload)", "تعديل اختبار" in isl2["shows"]["morning"]["system_prompt"])
c.put("/api/v1/agents/islamic/morning", json={"content": orig})  # restore
r3 = c.get("/api/v1/templates").get_json()
isl3 = next(s for s in r3["data"] if s["key"] == "islamic")
check("restore ok", "تعديل اختبار" not in isl3["shows"]["morning"]["system_prompt"])

print("guards:")
check("path traversal blocked", c.get("/api/v1/agents/..%2f../secret/x").status_code in (400, 404))
bad = c.put("/api/v1/agents/islamic/morning", json={"content": "---\nkey: [bad\n---\nx"}).get_json()
check("malformed frontmatter rejected", bad["ok"] is False)

print("\nRESULT:", "ALL PASS ✓" if ok else "SOME FAILED ✗")
sys.exit(0 if ok else 1)
