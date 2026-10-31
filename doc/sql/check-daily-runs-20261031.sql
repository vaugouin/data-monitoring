-- Read-only, run by the agent before the ops dashboard's first night: is data-monitoring
-- still running every morning? off-all.sh switched every task off on 2026-10-25, this one
-- included; if its last snapshot is that old, the ops page will not appear until on.sh.
-- Also: today's ops-health rows, if any (none before the first run).

SELECT REPORT_SLUG, MAX(DAT_CREAT) AS LAST_DAY, MAX(TIM_UPDATED) AS LAST_RUN, COUNT(*) AS ROWS_TOTAL
FROM T_WC_DATA_MONITORING_SNAPSHOT
WHERE DELETED = 0
GROUP BY REPORT_SLUG
ORDER BY LAST_RUN DESC;

SELECT TABLE_NAME, DONE_COUNT, DESCRIPTION, TIM_UPDATED
FROM T_WC_DATA_MONITORING_SNAPSHOT
WHERE REPORT_SLUG = 'ops-health' AND DAT_CREAT = CURDATE()
ORDER BY DISPLAY_ORDER;
