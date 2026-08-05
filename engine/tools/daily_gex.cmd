@echo off
rem Daily gamma-data refresh (scheduled task "Trading_GEX_Daily", Mon-Fri 15:00
rem local = 09:00 ET, 30 min before the US open):
rem   1. SqueezeMetrics aggregate GEX/DIX -> QuestDB claude_gex
rem   2. CBOE chain snapshot (true OI) -> flip/walls -> QuestDB claude_gex_levels
rem   3. VERIFY both tables actually advanced, and fail loudly if not.
rem Requires QuestDB running on localhost:9000. Log: D:\Trading\gamma\fetch_log.txt
rem
rem 2026-07-29: this ran every weekday and wrote NOTHING for 8 days without a
rem single visible error. The task's ExecutionTimeLimit was PT15M at Priority 7,
rem so on trading days -- NT8 + engine + QuestDB all loaded -- Windows killed it
rem mid-run (exit 0xC000013A, CTRL_C_EXIT). Because python buffers stdout when
rem it is redirected to a file, the kill discarded every diagnostic line and the
rem log showed only a bare timestamp header. Meanwhile GammaRegime happily served
rem the newest row it had, so every *_gex sleeve gated on 6-day-old gamma.
rem Hence: -u (never lose output to a kill), an explicit freshness check, and a
rem non-zero exit so the task's LastTaskResult reflects reality.
cd /d D:\Trading\engine
echo ==== %date% %time% ==== >> ..\gamma\fetch_log.txt
.venv\Scripts\python.exe -u tools\fetch_gex.py >> ..\gamma\fetch_log.txt 2>&1
.venv\Scripts\python.exe -u tools\fetch_cboe_gex.py >> ..\gamma\fetch_log.txt 2>&1
.venv\Scripts\python.exe -u tools\gex_freshness.py >> ..\gamma\fetch_log.txt 2>&1
exit /b %ERRORLEVEL%
