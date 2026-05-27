import sqlite3, sys, json
sys.stdout.reconfigure(encoding="utf-8")
con = sqlite3.connect("db/history.db")
rows = con.execute(
    "SELECT original_name, structure_json, output_path FROM documents WHERE original_name LIKE '%קול קורא%' ORDER BY created_at DESC LIMIT 3"
).fetchall()
for row in rows:
    name, sj, op = row
    print(f"\n=== {name} ===")
    print(f"output: {op}")
    if sj:
        data = json.loads(sj)
        print(f"structure_json elements: {len(data)}")
        for e in data[:5]:
            print(f"  {e.get('type')}: {repr(e.get('text','')[:60])}")
    else:
        print("structure_json: ריק/NULL")
