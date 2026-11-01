CREATE TABLE sc_governance.losing (email STRING MASK sc_governance.mask_a, region STRING)
WITH ROW FILTER sc_governance.eu_only ON (region);

GRANT SELECT, MODIFY ON TABLE sc_governance.losing TO `account users`;
