import sys; sys.path.insert(0, "/home/user/novahomedashboardminiapp")
from backend import database, crm, crm_ext, inbox
database.init_db(); inbox.init_db(); crm.init_db(); crm_ext.init_db()
assert crm_ext.unresolved("Elizaveta, инструкция по заселению в B-005: адрес — ..., код домофона — …, ключи — ....") == "..., …", crm_ext.unresolved("адрес — ..., код — …")
assert crm_ext.unresolved("Привет. Всё хорошо.") == ""
crm.set_setting("auto_step_messages", "0"); assert crm_ext.run_task_messages() == 0
crm.set_setting("auto_step_messages", "1"); crm_ext.run_task_messages()  # runs (sends nothing here: no due tasks with chats)
print("steps-off OK")
