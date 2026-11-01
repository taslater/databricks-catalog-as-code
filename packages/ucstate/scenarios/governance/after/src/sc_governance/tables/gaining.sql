CREATE TABLE sc_governance.gaining (email STRING MASK sc_governance.mask_a, region STRING)
WITH ROW FILTER sc_governance.eu_only ON (region);

GRANT SELECT, MODIFY ON TABLE sc_governance.gaining TO `account users`;
