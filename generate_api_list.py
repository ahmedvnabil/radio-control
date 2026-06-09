import ast
from pathlib import Path

endpoints = []
for file in ["app.py", "agents_engine.py", "telegram_engine.py", "studio_engine.py"]:
    if not Path(file).exists(): continue
    content = Path(file).read_text(encoding="utf-8")
    tree = ast.parse(content)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and getattr(dec.func, "attr", "") in ("get", "post", "put", "delete", "route"):
                    method = dec.func.attr.upper()
                    if method == "ROUTE":
                        try:
                            method = [k.value for k in dec.keywords if k.arg == "methods"][0].elts[0].value
                        except:
                            method = "GET"
                    path = dec.args[0].value if dec.args else ""
                    doc = ast.get_docstring(node) or ""
                    
                    azuracast = "N/A (Local / Native)"
                    for child in ast.walk(node):
                        if isinstance(child, ast.Call) and getattr(child.func, "id", "") == "_proxy":
                            try:
                                az_method = child.args[0].value
                            except:
                                az_method = "ANY"
                            
                            path_node = child.args[1]
                            if isinstance(path_node, ast.JoinedStr):
                                az_path = ""
                                for part in path_node.values:
                                    if isinstance(part, ast.Constant):
                                        az_path += str(part.value)
                                    elif isinstance(part, ast.FormattedValue):
                                        if isinstance(part.value, ast.Name):
                                            az_path += "{" + part.value.id + "}"
                                        else:
                                            az_path += "{...}"
                            elif isinstance(path_node, ast.Constant):
                                az_path = path_node.value
                            else:
                                az_path = "{dynamic}"
                            
                            azuracast = f"{az_method} {az_path}"
                            break

                    category = "Data Input & Actions (POST, PUT, DELETE)" if method in ("POST", "PUT", "DELETE") else "Data Output & Retrieval (GET)"
                    endpoints.append({
                        "method": method,
                        "path": path,
                        "doc": doc.split("\n")[0] if doc else "No description",
                        "category": category,
                        "func": node.name,
                        "azuracast": azuracast
                    })

# Sort endpoints by path alphabetically for better presentation
endpoints.sort(key=lambda x: x["path"])

# Generate HTML
html = """<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
  <meta charset="utf-8">
  <title>تصنيف نقاط الاتصال (API Endpoints)</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-900 text-gray-100 p-8 font-sans">
  <div class="max-w-6xl mx-auto">
    <h1 class="text-3xl font-bold text-blue-400 mb-6">قائمة وظائف الـ API الخاصة بتطبيق الراديو</h1>
    <p class="mb-8 text-gray-400 text-lg leading-relaxed">هذه القائمة تفصل بين الـ Endpoints التي تقوم بإرجاع بيانات فقط (Outputs) والـ Endpoints التي تستقبل بيانات وتنفذ أوامر (Inputs & Actions). كما تتضمن المقارنة مع المسارات الخاصة بـ AzuraCast.</p>
"""

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
        az_color = "text-gray-500" if ep["azuracast"].startswith("N/A") else "text-purple-400 font-bold"
        
        html += f'''
        <div class="bg-gray-800 p-4 rounded-lg border border-gray-700 hover:border-gray-500 transition-colors grid grid-cols-1 md:grid-cols-2 gap-4">
          <!-- Radio Control Side -->
          <div>
            <div class="text-xs text-gray-500 mb-1">Radio Control Endpoint</div>
            <div class="flex items-center gap-3 mb-2" dir="ltr">
              <span class="font-bold {m_color} text-sm px-2 py-1 bg-gray-900 rounded border border-gray-700">{ep["method"]}</span>
              <span class="font-mono text-gray-300 font-semibold">{ep["path"]}</span>
            </div>
            <p class="text-gray-400 text-sm mt-2"><span class="text-gray-500">الوصف:</span> {desc}</p>
          </div>
          
          <!-- AzuraCast Side -->
          <div class="border-t md:border-t-0 md:border-r border-gray-700 pt-4 md:pt-0 md:pr-4">
            <div class="text-xs text-gray-500 mb-1" dir="rtl">المسار الموازي في (AzuraCast)</div>
            <div class="flex items-center gap-3 mb-2" dir="ltr">
              <span class="font-mono {az_color} text-sm px-2 py-1 bg-gray-900 rounded border border-gray-700">{ep["azuracast"]}</span>
            </div>
          </div>
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
