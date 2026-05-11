@echo off
cd /d C:\Users\D_Reyes\Desktop\legacy
echo ==== %date% %time% ==== >> output\kpi_sync_cron.log
python .\sync_near_real_time_google.py --active-only >> output\kpi_sync_cron.log 2>&1
