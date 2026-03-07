insert INTO donations(server_id, user_id, item, donation_date, quantity)
values(
    1466549361432461436,373173298189565952,'Gambling Token',now(),-170775

)

;

insert into bot_settings(server_id,gambling_channel)
values(
    1466549361432461436,1479875157592899655
)
on CONFLICT (server_id)
do update set gambling_channel = 1479875157592899655
;
select * from bot_settings