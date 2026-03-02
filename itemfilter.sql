
-- select * from donation_values where 
--  server_id <> 1466549361432461436

INSERT INTO donation_values (server_id, item_name, donation_value)
VALUES (1, 'test', 1.4);
select * from donation_values where item_name = 'test'