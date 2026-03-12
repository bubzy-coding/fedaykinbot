

SELECT d.user_id,
                SUM(d.quantity * i.donation_value) AS total_value
                FROM donations d
                JOIN donation_values i ON d.item = i.item_name
                WHERE d.server_id = 1466549361432461436
                    AND d.donation_date >= '2026-03-10'
                    AND NOT d.is_adjustment
                    AND i.server_id = 1466549361432461436
                GROUP BY d.user_id
                ORDER BY total_value DESC;