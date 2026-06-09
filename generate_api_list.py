import ast
from pathlib import Path

endpoints = []
for file in ["app.py", "agents_engine.py", "telegram_engine.py", "studio_engine.py"]:
    content = Path(file).read_text(encoding="utf-8")
    tree = ast.parse(content)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and getattr(dec.func, "attr", "") in ("get", "post", "put", "delete", "route"):
                    method = dec.func.attr.upper()
                    if method == "ROUTE":
                        # naive extraction
                        try:
                            method = [k.value for k in dec.keywords if k.arg == "methods"][0].elts[0].value
                        except:
                            method = "GET"
                    path = dec.args[0].value if dec.args else ""
                    doc = ast.get_docstring(node) or ""
                    
                    category = "Data Input & Actions (POST, PUT, DELETE)" if method in ("POST", "PUT", "DELETE") else "Data Output & Retrieval (GET)"
                    endpoints.append({
                        "method": method,
                        "path": path,
                        "doc": doc.split("\n")[0] if doc else "No description",
                        "category": category,
                        "func": node.name
                    })

# Generate HTML
html = """<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
  <meta charset="utf-8">
  <title>تصنيف نقاط الاتصال (API Endpoints)</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-900 text-gray-100 p-8 font-sans">
  <div class="max-w-5xl mx-auto">
    <h1 class="text-3xl font-bold text-blue-400 mb-6">قائمة وظائف الـ API الخاصة بتطبيق الراديو</h1>
    <p class="mb-8 text-gray-400 text-lg leading-relaxed">هذه القائمة تفصل بين الـ Endpoints التي تقوم بإرجاع بيانات فقط (Outputs) والـ Endpoints التي تستقبل بيانات وتنفذ أوامر (Inputs & Actions).</p>
"""

# Group by category
groups = {"Data Input & Actions (POST, PUT, DELETE)": [], "Data Output & Retrieval (GET)": []}
for ep in endpoints:
    if "category" in ep:
        groups[ep["category"]].append(ep)

for cat_name, eps in groups.items():
    title_ar = "نقط إدخال البيانات وتنفيذ الأوامر (تستقبل طلب)" if "Input" in cat_name else "نقاط استرجاع وعرض البيانات (تُخرج بيانات)"
    color = "text-green-400" if "Input" in cat_name else "text-blue-400"
    
    html += f'<h2 class="text-2xl font-bold {color} mt-10 mb-4 border-b border-gray-700 pb-2">{title_ar}</h2>\n'
    html += '<div class="grid gap-4">\n'
    
    for ep in eps:
        m_color = "text-blue-500" if ep["method"]=="GET" else ("text-green-500" if ep["method"]=="POST" else "text-yellow-500" if ep["method"]=="PUT" else "text-red-500")
        desc = ep["doc"].replace('"', '&quot;').replace('<', '&lt;').replace('>', '&gt;')
        html += f'''
        <div class="bg-gray-800 p-4 rounded-lg border border-gray-700 hover:border-gray-500 transition-colors">
          <div class="flex items-center gap-3 mb-2" dir="ltr">
            <span class="font-bold {m_color} text-sm px-2 py-1 bg-gray-900 rounded border border-gray-700">{ep["method"]}</span>
            <span class="font-mono text-gray-300 font-semibold">{ep["path"]}</span>
          </div>
          <p class="text-gray-400 text-sm mt-2"><span class="text-gray-500">الوصف:</span> {desc}</p>
        </div>
        '''
    html += "</div>\n"

html += """
  </div>
</body>
</html>
"""

Path("templates/apiendpoints.html").write_text(html, encoding="utf-8")
print(f"Generated apiendpoints.html with {len(endpoints)} endpoints.")
