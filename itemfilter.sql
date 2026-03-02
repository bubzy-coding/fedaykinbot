SELECT atttypmod
FROM pg_attribute
WHERE attrelid = 'donation_values'::regclass
AND attname = 'donation_value';