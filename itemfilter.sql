SELECT *
FROM items_new
WHERE short_description <> 'TBD'
AND EXISTS (
    SELECT 1
    FROM jsonb_array_elements_text(item_tags) AS tag
    WHERE tag LIKE 'Items.CraftedResources%'
    OR tag LIKE 'Items.RefinedResources%'
    OR tag LIKE 'Items.RawResources%'
)
AND NOT EXISTS (
    SELECT 1
    FROM jsonb_array_elements_text(item_tags) AS tag
    WHERE tag = 'Items.Consumables.BuildableSets'
)
UNION 
select * from extra_items