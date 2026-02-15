import json

with open("items_list.json", "r", encoding="utf-8") as f:
    data = json.load(f)

rows = []

for item in data:
    escaped = item.replace("'", "''")
    rows.append(f"('{escaped}', FALSE)")

values = ",\n".join(rows)

sql = f"""INSERT INTO items (item_name, is_contributable)
VALUES
{values};
"""

with open("sql_text.sql", "w") as f:
    f.write(sql)
