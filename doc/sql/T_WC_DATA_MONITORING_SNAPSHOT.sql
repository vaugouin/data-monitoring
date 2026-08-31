-- =============================================================================
-- data-monitoring — history table
-- One row per (snapshot day, source DB, table, metric). Append-only history;
-- never pruned (it is the trend). Written by the monitoring_ro user with
-- INSERT + UPDATE only (the UPDATE branch serves the idempotent same-day re-run
-- via ON DUPLICATE KEY UPDATE on UK_SNAPSHOT_DAY).
--
-- Standard columns follow doc-tech "Structure type d'une table simple"
-- (%USERPROFILE%/Code/webathenkel-docs/doc/doc-tech-20110223.md). Run once with a CREATE-privileged user.
-- =============================================================================

CREATE TABLE `T_WC_DATA_MONITORING_SNAPSHOT` (
  `ID_SNAPSHOT`     int(11) NOT NULL AUTO_INCREMENT,
  -- what is being measured
  `REPORT_SLUG`     varchar(100) DEFAULT NULL,   -- 'tmdb-tv-coverage'
  `SOURCE_DB`       varchar(50)  DEFAULT NULL,    -- 'TMDB' | 'WIKIDATA' | ...
  `TABLE_NAME`      varchar(100) DEFAULT NULL,    -- 'T_WC_TMDB_EPISODE'
  `METRIC_KEY`      varchar(100) DEFAULT NULL,    -- 'episode_series_completion'
  -- the measured values
  `DONE_COUNT`      bigint(20)   DEFAULT NULL,
  `EXPECTED_COUNT`  bigint(20)   DEFAULT NULL,
  `PCT`             decimal(6,2) DEFAULT NULL,    -- 0.00..100.00
  `DAILY_RATE`      double       DEFAULT NULL,    -- rows added on the snapshot day
  -- standard columns ("Structure type d'une table simple")
  `DESCRIPTION`     varchar(250) DEFAULT NULL,    -- human label of the metric
  `LONG_DESC`       mediumtext   DEFAULT NULL,    -- definition / how to read NULL/zero
  `DELETED`         int(5)       DEFAULT 0,
  `DISPLAY_ORDER`   int(5)       DEFAULT NULL,
  `ID_CREATOR`      int(5)       DEFAULT NULL,
  `DAT_CREAT`       date         DEFAULT NULL,    -- snapshot day (grouping key)
  `ID_OWNER`        int(5)       DEFAULT NULL,
  `TIM_UPDATED`     datetime     DEFAULT NULL,    -- exact run timestamp
  `ID_USER_UPDATED` int(5)       DEFAULT NULL,
  PRIMARY KEY (`ID_SNAPSHOT`),
  UNIQUE KEY `UK_SNAPSHOT_DAY` (`DAT_CREAT`,`SOURCE_DB`,`TABLE_NAME`,`METRIC_KEY`),
  KEY `REPORT_SLUG` (`REPORT_SLUG`),
  KEY `SOURCE_DB` (`SOURCE_DB`),
  KEY `TABLE_NAME` (`TABLE_NAME`),
  KEY `METRIC_KEY` (`METRIC_KEY`),
  KEY `DELETED` (`DELETED`),
  KEY `DISPLAY_ORDER` (`DISPLAY_ORDER`),
  KEY `ID_CREATOR` (`ID_CREATOR`),
  KEY `ID_OWNER` (`ID_OWNER`),
  KEY `ID_USER_UPDATED` (`ID_USER_UPDATED`),
  KEY `DAT_CREAT` (`DAT_CREAT`),
  KEY `TIM_UPDATED` (`TIM_UPDATED`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
