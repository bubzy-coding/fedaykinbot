import json

with open("items.json", "r", encoding="utf-8") as f:
    data = json.load(f)

names = []

for entry in data:
    if isinstance(entry, dict) and "mainCategoryId" in entry and "name" in entry:

        # Resolve mainCategoryId once
        cat_index = entry["mainCategoryId"]
        if isinstance(cat_index, int) and cat_index < len(data):
            category_value = data[cat_index]
      
            if category_value == "items":  # <-- your filter

                # Resolve name once
                name_index = entry["name"]
      
                if isinstance(name_index, int) and name_index < len(data):
                    name_value = data[name_index]

                    if isinstance(name_value, str) and not name_value.endswith(":"):
                        names.append(name_value)

#print(names[:10])
with open("items_list.json", "w", encoding="utf-8") as f:
    json.dump(names, f, indent=2)