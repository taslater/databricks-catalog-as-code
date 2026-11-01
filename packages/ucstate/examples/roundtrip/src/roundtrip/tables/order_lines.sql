CREATE TABLE roundtrip.order_lines (
  `order` BIGINT NOT NULL COMMENT 'O\'Brien\'s order, with a \\ backslash',
  `Line Number` INT,
  event_date DATE,
  attrs map<string,string>,
  dims struct<width:double,Height:double>,
  labels array<string>
)
PARTITIONED BY (event_date)
COMMENT 'Lines; partitioned by date'
TBLPROPERTIES (
  'delta.columnMapping.mode' = 'name'
);
