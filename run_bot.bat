@echo off
cd /d C:\Users\User\TheBot
call .venv\Scripts\activate
set PGCLIENTENCODING=UTF8
python bot.py --user igor --bot-id btc_paper_01