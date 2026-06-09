"""Integration test for the merged Studio API, Day Blocks API, and Bilingual tools."""
import os
import sys
import json
from pathlib import Path

os.environ["AZURACAST_API_KEY"] = ""
os.environ["WERKZEUG_RUN_MAIN"] = "false"
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.pop("ANTHROPIC_API_KEY", None)  # run without live calls

import app as appmod
import studio_tools

ok = True


def check(label, cond):
    global ok
    ok = ok and cond
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


c = appmod.app.test_client()

print("Bilingual Tools:")
# English syllables check
en_syl = studio_tools.count_syllables("hello world")
check("English syllables count", en_syl["total"] == 3)  # hel-lo world = 3

# Arabic syllables check (with harakat)
ar_syl_harakat = studio_tools.count_syllables("مَدْرَسَة")
check("Arabic syllables count (harakat)", ar_syl_harakat["total"] == 3)  # م َ د ْ ر َ س َ ة = 3 harakat count (excluding sukun)

# Arabic syllables check (unvocalized)
ar_syl_unvocalized = studio_tools.count_syllables("مدرسة")
check("Arabic syllables count (unvocalized)", ar_syl_unvocalized["total"] == 2)  # 5 chars // 2 = 2

# Arabic rhyme check
rhyme_ar = studio_tools.check_rhyme("جميل", "طويل")
check("Arabic rhyme check (rhymes)", rhyme_ar["rhymes"] is True)

rhyme_ar_bad = studio_tools.check_rhyme("جميل", "مدرسة")
check("Arabic rhyme check (no rhyme)", rhyme_ar_bad["rhymes"] is False)


p_file = Path("agents/day_builder/1.json").resolve()
try:
    p_file.unlink(missing_ok=True)
except Exception as e:
    print("DEBUG unlink error:", e)

print("Day Blocks API:")
# Test GET day blocks (empty fallback)
r_get = c.get("/api/v1/stations/1/day-blocks").get_json()
check("GET empty day blocks ok", r_get["ok"] and r_get["data"] == [])

# Test POST/PUT day-blocks
# api_day_blocks_put expects a list of dictionaries with id, type, title, startHour, durationMins, and data fields
blocks_data = [{"id": "b1", "type": "music", "title": "Song 1", "startHour": 8, "durationMins": 5, "data": None, "showKey": None}]
r_put = c.put("/api/v1/stations/1/day-blocks", json={"blocks": blocks_data}).get_json()
check("PUT day blocks ok", r_put["ok"])

r_get2 = c.get("/api/v1/stations/1/day-blocks").get_json()
check("GET updated day blocks ok", r_get2["ok"] and r_get2["data"] == blocks_data)

# Cleanup day blocks file
p_file.unlink(missing_ok=True)


print("Studio Agents API:")
r_agents = c.get("/api/v1/studio/agents").get_json()
check("GET /studio/agents ok", r_agents["ok"] and len(r_agents["data"]) == 3)

# Test GET one agent
r_one = c.get("/api/v1/studio/agents/lyric-writer").get_json()
check("GET lyric-writer agent content", r_one["ok"] and "lyric-writer" in r_one["data"]["content"])

# Test PUT edit round-trip
orig_content = r_one["data"]["content"]
edited_content = orig_content + "\n# Test edit"
r_save = c.put("/api/v1/studio/agents/lyric-writer", json={"content": edited_content}).get_json()
check("PUT save agent ok", r_save["ok"])

r_one_updated = c.get("/api/v1/studio/agents/lyric-writer").get_json()
check("GET updated agent content", r_one_updated["ok"] and "Test edit" in r_one_updated["data"]["content"])

# Restore original content
c.put("/api/v1/studio/agents/lyric-writer", json={"content": orig_content})

# Test Run Agent (graceful auth fail without key)
r_run = c.post("/api/v1/studio/run", json={"agent": "qc-checker", "input": "test input"}).get_json()
check("POST run returns NO_API_KEY gracefully", r_run["ok"] is False and r_run["code"] == "NO_API_KEY")

print("\nRESULT:", "ALL PASS ✓" if ok else "SOME FAILED ✗")
sys.exit(0 if ok else 1)
