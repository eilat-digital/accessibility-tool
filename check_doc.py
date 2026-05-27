import sqlite3, sys, json
sys.stdout.reconfigure(encoding="utf-8")
con = sqlite3.connect("db/history.db")
row = con.execute("SELECT original_name, accessibility_features FROM documents WHERE output_path LIKE '%be3452f6%'").fetchone()
print("Name:", row[0] if row else "not found")
if row and row[1]:
    feats = json.loads(row[1])
    print("Features:", json.dumps(feats, ensure_ascii=False, indent=2)[:500])
