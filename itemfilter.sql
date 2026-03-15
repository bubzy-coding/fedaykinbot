 WITH current_inventory AS (
                SELECT
                    server_id,
                    upper(item_name) as item_name,
                    sum(quantity) AS stock_qty
                FROM inventory
                GROUP BY server_id, item_name
            )
            SELECT
                r.item_name,
                r.required_quantity,
                sum(COALESCE(i.stock_qty, 0)) AS stock_qty,
                r.required_quantity - sum(COALESCE(i.stock_qty, 0)) AS remaining
            FROM required_items r
            LEFT JOIN current_inventory i ON i.server_id = r.server_id
            AND i.item_name = upper(r.item_name)
            WHERE r.server_id = $1
            and r.required_quantity >0
            GROUP BY r.item_name, r.required_quantity
            ORDER BY r.item_name
            LIMIT 20;
            