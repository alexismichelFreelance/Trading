@echo off
rem Daily gamma-data refresh (scheduled task "Trading_GEX_Daily", Mon-Fri 15:00
rem local = 09:00 ET, 30 min before the US open):
rem   1. SqueezeMetrics aggregate GEX/DIX -> QuestDB claude_gex
rem   2. CBOE chain snapshot (true OI) -> flip/walls -> QuestDB claude_gex_levels
rem Requires QuestDB running on localhost:9000. Log: D:\Trading\gamma\fetch_log.txt
cd /d D:\Trading\engine
echo ==== %date% %time% ==== >> ..\gamma\fetch_log.txt
.venv\Scripts\python.exe tools\fetch_gex.py >> ..\gamma\fetch_log.txt 2>&1
.venv\Scripts\python.exe tools\fetch_cboe_gex.py >> ..\gamma\fetch_log.txt 2>&1
